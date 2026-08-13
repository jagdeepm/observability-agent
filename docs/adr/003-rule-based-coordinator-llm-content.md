# ADR-003: Rule-Based Coordinator with LLM-Filled Content

**Status:** Accepted
**Date:** 2026-07-21

## Context

The Coordinator must drive an Incident through its lifecycle: exploratory evidence gathering → hypothesis generation → targeted investigation → hypothesis evaluation → deepening → RCA production. The orchestration layer has two possible architectures:

1. **LLM-driven orchestration.** An LLM loop decides which step to take next based on the current state. The LLM is the control plane.
2. **Rule-based state machine with LLM-filled content.** The workflow structure is hardcoded. The LLM is called only for content generation (hypothesis statements, evidence queries, confidence scores, RCA narrative).

## Decision

**Use a rule-based state machine with LLM-filled content.** The `Coordinator.run()` method executes a deterministic sequence of phases. The LLM is called for content, not for control flow.

### Phase sequence

```
open → acknowledge → explore → generate hypotheses → investigate (loop) → produce RCA
```

The investigate loop:
1. Find unresolved hypotheses
2. Gather targeted evidence for each (keyword heuristic routes to agent)
3. Batch-evaluate all unresolved hypotheses in one LLM call
4. Prune refuted branches
5. Deepen confirmed leaf hypotheses via LLM
6. Repeat until convergence or `max_depth`

### What the LLM does

| Phase | LLM call | Output schema |
|---|---|---|
| Generate hypotheses | Review exploratory evidence → propose hypotheses | `GenerateHypothesesOutput` |
| Evaluate hypotheses | Review evidence for each hypothesis → confirm or refute | `EvaluateHypothesesOutput` |
| Deepen hypotheses | Drill into confirmed hypothesis → propose child hypotheses | `DeepenHypothesesOutput` |
| Produce RCA | Synthesize confirmed path → narrative + remediation | `RCAOutput` |

### What the LLM does NOT do

- Decide which step comes next
- Choose which agent to call
- Decide when to stop
- Manage the Incident state machine

## Consequences

### Positive

- **Predictable.** The workflow is deterministic. The same input always produces the same sequence of steps. Debugging is straightforward.
- **Cost-effective.** LLM calls are bounded — at most 4 distinct call types per investigation, not an unbounded agent loop.
- **Testable.** The rule-based structure can be tested with mock LLM responses. Tests verify mechanics, not LLM judgment.
- **Debuggable.** When something goes wrong, you can trace the exact phase. No "the LLM decided to skip evaluation" mysteries.
- **Progressive context.** Each LLM call gets only the data it needs. Raw telemetry payloads are never sent to the LLM.

### Negative

- **Less flexible.** An LLM might discover a better investigation strategy for a specific incident. The rule-based approach cannot adapt its workflow.
- **Keyword heuristic is simple.** The `_pick_source` heuristic routes hypotheses to agents by keyword matching. An LLM could make more nuanced routing decisions. Mitigated by keeping the heuristic simple and documented — a future ADR can replace it with an LLM call.

## Alternatives Not Chosen

- **LLM-driven orchestration (LangGraph, agent loop).** The LLM decides each step. More flexible but more expensive, less predictable, and harder to test. Rejected for MVP because the rule-based approach covers the common case. LLM-driven orchestration can be added as an optional mode later.
- **Fully rule-based without LLM.** Would require pre-written investigation templates for every alert type. Not scalable. The LLM is essential for generating hypotheses and evaluating evidence from arbitrary telemetry data.