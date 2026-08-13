# ADR-002: Hypothesis Tree as Investigation Model with RCA as Projection

**Status:** Accepted
**Date:** 2026-07-20

## Context

The investigation engine needs a structured way to model root-cause analysis. The system must:

- Support causal reasoning from broad symptoms to specific root causes
- Allow multiple competing hypotheses to be explored in parallel
- Cascade-refute downstream hypotheses when a parent is disproven
- Produce an RCA that is always consistent with the underlying evidence
- Avoid duplicating state between the investigation and its output

## Decision

**Use a hypothesis tree with parent-child adjacency.** The RCA is a **projection** over the tree — the confirmed path with the highest-confidence leaf, bundled with supporting evidence. The RCA is not a separate entity.

### Data model

```
Hypothesis
├── id: str
├── parent_id: str | None      ← parent-child adjacency
├── statement: str
├── status: proposed | investigating | confirmed | refuted
├── confidence: 0.0–1.0
└── evidence: list[Evidence]   ← linked by hypothesis_id
```

### Tree operations

| Operation | Behavior |
|---|---|
| `ancestor_chain(all_hypotheses)` | Walk from any hypothesis to the root |
| `confirmed_path(all_hypotheses)` | Chain from root to leaf where every link is confirmed. Returns `None` if broken. |
| `prune_refuted_branches()` | Cascade-refute all children of refuted hypotheses |
| `rca_report()` | Projection: find best confirmed path → bundle with evidence → return dict |

### Why RCA is a projection, not an entity

If the RCA were a separate entity, it would duplicate the hypothesis tree state. Every time a hypothesis was confirmed or refuted, the RCA would need to be updated. With a projection, the RCA is always consistent with the investigation state — it's computed on demand from the live hypothesis tree.

## Consequences

### Positive

- **Single source of truth.** The hypothesis tree is the only state. The RCA is derived, not stored.
- **Cascading refutation.** When a parent is refuted, children are automatically pruned. This prevents wasted effort on dead branches.
- **Natural depth limit.** The tree depth naturally bounds investigation. The Coordinator stops when no new children can be generated or `max_depth` is reached.
- **Confidence flows upward.** A confirmed chain from root to leaf represents a complete causal narrative. Confidence scores aggregate naturally.

### Negative

- **No explicit "RCA" entity.** The RCA is a dict, not a typed model. A future `StructuredRCA` Pydantic model could strengthen the contract without changing the architecture.
- **Tree can be deep.** A `max_depth=3` default prevents runaway deepening, but complex incidents may need more depth. The configurable `max_depth` parameter addresses this.

## Alternatives Not Chosen

- **Flat list of hypotheses.** Would not support causal chains or pruning. Rejected — too simple for complex incidents.
- **RCA as separate entity.** Would duplicate state and risk inconsistency. The projection approach keeps the model normalized.
- **DAG (directed acyclic graph).** A hypothesis could have multiple parents (converging causal chains). Over-engineering for MVP — the tree model captures the common case and a DAG can be added later if needed.