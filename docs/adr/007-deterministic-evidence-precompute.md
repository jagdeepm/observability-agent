# ADR-007: Deterministic Pre-Compute of Telemetry Evidence

**Status:** Accepted
**Date:** 2026-08-14

## Context

Each agent followed the same pipeline: translate the query, execute it, hand the raw response to the LLM, get back a summary. The hand-off was a stringified payload truncated at 4,000 characters, and the structured output schemas asked the LLM for numeric facts alongside the prose:

- `MetricsSummaryOutput.anomaly_detected: bool` — is there an anomaly?
- `LogsSummaryOutput.error_count: int` — how many error lines?
- `TracesSummaryOutput.slowest_service` / `.bottleneck_span` — which span is slowest?

These are a threshold test, a count, and a maximum. Delegating them to a language model reading a partial view of the data produces confident answers about data the model cannot see: a Prometheus range query or a burst of identical log lines exceeds the truncation budget long before it stops being interesting.

Two consequences followed. A Hypothesis could be confirmed or refuted on a count that was never counted. And because `prompts.py` renders only `Evidence.summary`, values written into `raw_payload` reached the Coordinator only if the LLM happened to restate them in prose.

Prior art: OpenChoreo's [SRE agent](https://github.com/openchoreo/openchoreo/tree/main/agents/sre-agent) computes statistics, detects anomalies, builds span trees, and groups logs in an `OutputTransformerMiddleware` before results reach its model. The idea is theirs.

## Decision

**Compute the facts in code; ask the LLM only for the language.**

A new `analysis.py` module analyses Prometheus, Loki, and Tempo payloads at the agent boundary — after the MCP client returns, before the summarization prompt is built. It computes distribution statistics with z-score and rate-of-change spike detection, deduplicated log groups with exact counts and time bounds, and a span tree ranked by self time. The rendered analysis replaces the truncated payload in the prompt.

The computed facts also travel on a new `Evidence.signals` field and are rendered into every Coordinator prompt by a single `format_evidence` helper. The fields listed above were removed from the agent output schemas.

### Divergence from the prior art

- **Placement.** OpenChoreo intercepts tool results inside a ReAct loop, because that is where their data flows. We have no tool-call loop to intercept: analysis is a fourth step in the pipeline ADR-004 already defines.
- **No array library.** They use numpy. A Prometheus range query returns hundreds of points, so the standard library's `statistics` covers every computation here and the dependency list stays at three packages.
- **Self time, not total duration.** A parent span's duration contains its children's, so ranking spans by total duration returns the root span every time and identifies nothing. We rank by self time.

### Scope

Kubernetes, deployment, and documentation evidence keep the truncated hand-off; their payloads are short and semi-structured, and RAG chunks are prose where truncating is correct. Each retained site carries a comment saying so.

## Consequences

**Positive.**

- Facts the Coordinator branches on are measured rather than inferred. A 415-line log burst reaches the LLM as four lines with exact counts.
- Prompt size is bounded by the number of distinct findings rather than by payload volume, so the noisiest incidents stop being the most expensive.
- The LLM output schemas now contain only what a language model should produce, which makes the computation/generation boundary legible from the schema alone.
- This deepens ADR-003 rather than departing from it: more of the pipeline is deterministic and testable.

**Negative.**

- `analysis.py` parses three vendor payload shapes and is coupled to them. Tempo alone returns attributes as a mapping on some endpoints and an OTLP key/value list on others; both are handled, and a fourth shape would need adding.
- Anomaly detection is a threshold heuristic, not a model of normal. It has no seasonality or baseline awareness, and the z-rule cannot fire on series of ten points or fewer, where the largest possible |z| is (n−1)/√n < 3. The rate-of-change rule covers short windows. Both thresholds are named constants intended for calibration.
- The Coordinator does not yet act on signals mechanically — they inform the LLM's evaluation. Rule-based pruning directly from signals is deferred as a decision of its own.

## Alternatives Considered

**Keep asking the LLM, just send better-formatted payloads.** Cheaper, and the token win would be similar. Rejected: it leaves a guessed boolean at the point where the Coordinator makes decisions, which is the actual defect.

**Replace the summarization call entirely and use rendered facts as the summary.** Saves an LLM call per evidence gather. Rejected: downstream hypothesis and RCA prompts would consume robotic text, degrading the reasoning that matters most.

**Adopt a ReAct loop with middleware, as the prior art does.** Rejected — see ADR-003. The comparison strengthened that decision rather than weakening it.
