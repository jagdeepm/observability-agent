# ADR-001: Pydantic v2 for Domain Models and Structured Outputs

**Status:** Accepted
**Date:** 2026-07-20

## Context

The observability agent needs a domain model that is:

- **Validated at creation time** — invalid states must be impossible to represent
- **Serializable** — incidents must be persisted to a database and transmitted over HTTP
- **Type-safe** — the Coordinator and agents must have clear contracts
- **LLM-compatible** — the LLM's structured outputs must be parseable into typed objects

Python alternatives considered:

| Option | Validation | Serialization | Type safety | LLM integration |
|---|---|---|---|---|
| `dataclasses` | Manual | Manual | Type hints only | Manual parsing |
| `attrs` | Manual validators | `cattrs` | Type hints | Manual parsing |
| Pydantic v2 | Built-in | Built-in (`.model_dump()`) | Full | `pydantic-ai` native |
| Plain dicts | None | `json` | None | JSON schema manual |

## Decision

**Use Pydantic v2 for all domain entities and all LLM structured outputs.**

- `BaseModel` for domain entities (`Incident`, `Hypothesis`, `Evidence`, `Remediation`, `Alert`, `Node`, `Edge`, `ResolvedIncident`)
- `BaseModel` for LLM output contracts (`GenerateHypothesesOutput`, `EvaluateHypothesesOutput`, `DeepenHypothesesOutput`, `RCAOutput`)
- `BaseModel` for agent-internal outputs (`PromQLOutput`, `LogQLOutput`, `MetricsSummaryOutput`, etc.)

## Consequences

### Positive

- **Validation at the boundary.** Pydantic validates on construction. A `Hypothesis` with `confidence=1.5` raises immediately — no silent corruption.
- **Serialization is free.** `model_dump(mode="json")` gives JSON-safe dicts for HTTP responses and database storage.
- **LLM integration.** `pydantic-ai` agent's `output_type` parameter accepts `BaseModel` subclasses directly. The LLM returns validated instances.
- **IDE support.** Type hints flow through the entire codebase. Autocomplete works on domain objects.
- **JSON Schema generation.** Pydantic can generate JSON Schema for structured outputs — useful for LLM function calling.

### Negative

- **Dependency weight.** Pydantic is a non-trivial dependency. Acceptable because the project already depends on `pydantic-ai`.
- **`BaseModel` is mutable.** Domain entities use `Field(default_factory=...)` for mutable defaults. State-transition methods are explicit (`incident.acknowledge()`), not hidden behind property setters.

## Alternatives Not Chosen

- **Plain `dataclasses`**: Would require manual validation, serialization, and JSON schema generation. Too much boilerplate.
- **`attrs` + `cattrs`**: Valid at model scale but adds two dependencies and doesn't integrate with `pydantic-ai`.
- **Plain dicts**: No validation, no type safety, no IDE support. Rejected for the core domain.