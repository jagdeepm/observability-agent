# Spec: Deterministic Evidence Pre-Compute

**Status:** ready-for-agent

## Problem Statement

An on-call engineer relies on the agent's RCA to decide whether to page someone, roll back a deploy, or go back to sleep. That RCA is only as good as the Evidence beneath it — and today the Evidence is partly guesswork.

Every agent hands its source's raw response to the LLM as a stringified payload truncated at a fixed character budget, then asks the LLM to both describe it *and* extract numeric facts from it. Three of those extractions are arithmetic dressed up as language:

- The MetricsAgent asks the LLM whether the series contains an anomaly.
- The LogsAgent asks the LLM to count error-level lines.
- The TraceAgent asks the LLM to identify the slowest service and the bottleneck span.

Each is a computation — a threshold test, a count, a maximum — delegated to a language model reading a partial view of the data. When a Prometheus range query or a burst of identical log lines exceeds the truncation budget, the model is answering about data it cannot see, and it answers confidently either way. A hypothesis can be confirmed or refuted on a count that was never counted.

Compounding this, the computed values are written into the Evidence raw payload, which no prompt ever renders. Only the Evidence summary text reaches the Coordinator. So even a correct value influences hypothesis evaluation, pruning, and the RCA projection only if the LLM happened to restate it in prose.

## Solution

Compute the numbers in code; let the LLM write the language.

Before an agent asks the LLM to summarize anything, its source response passes through a deterministic analysis step that computes the facts a human SRE would compute by hand — distribution statistics and anomaly counts for metrics, deduplicated message groups with exact counts and time bounds for logs, a reconstructed span tree with the slowest span surfaced for traces. That rendered analysis, not a truncated string dump, becomes the LLM's input.

The computed facts are also attached to the Evidence as structured signals and rendered into every Coordinator prompt beside the summary. A hypothesis is then evaluated against a measured value, not a paraphrase of one.

The fields that asked the LLM for numbers are removed from the agent output schemas. The LLM's remaining job is the one it is good at: turning facts into a readable sentence.

This is a deepening of ADR-003, not a departure from it. The rule-based Coordinator with LLM-filled content gains more rule and less LLM, in exactly the place where determinism is cheap and hallucination is expensive.

## User Stories

1. As an on-call SRE, I want the anomaly determination on metric Evidence to be computed from the full series, so that a hypothesis is never confirmed on an anomaly the LLM inferred from a truncated payload.
2. As an on-call SRE, I want error-line counts in log Evidence to be exact, so that "17 errors" and "1,700 errors" lead me to different conclusions rather than the same vague sentence.
3. As an on-call SRE, I want the slowest span in a trace identified by comparing durations, so that the bottleneck I chase is the real one.
4. As an on-call SRE, I want repeated identical log lines collapsed into a single entry with an occurrence count and first/last timestamps, so that a crash loop reads as one finding rather than scrolling noise.
5. As an on-call SRE, I want trace Evidence to preserve parent/child structure, so that I can see where in the call chain the latency accumulates instead of a flat list of spans.
6. As an on-call SRE, I want percentile statistics on metric Evidence, so that I can distinguish a sustained regression from a single outlying scrape.
7. As an on-call SRE, I want spike counts and magnitudes reported, so that I can tell an intermittent stutter from a step change.
8. As an on-call SRE, I want the measured facts shown alongside the narrative summary in the RCA, so that I can check the agent's reasoning against its data without opening a dashboard.
9. As an on-call SRE, I want the Coordinator to evaluate a Hypothesis against measured values, so that confirmation and refutation rest on evidence rather than on the LLM's restatement of evidence.
10. As an on-call SRE, I want a refuted branch of the Hypothesis tree to have been refuted for a checkable reason, so that I can trust the pruning rather than re-investigating it myself.
11. As an on-call SRE, I want the full untruncated source response retained on the Evidence, so that I can audit any computed fact after the incident.
12. As an on-call SRE, I want an agent whose source returned nothing to say so plainly, so that absent data is never mistaken for healthy data.
13. As an incident reviewer, I want archived incidents in the Memory store to carry their computed signals, so that similarity search across past incidents compares facts and not prose.
14. As an incident reviewer, I want the RCA projection to cite specific measured values, so that a postmortem can be written from the report without re-deriving the numbers.
15. As a maintainer, I want the statistics computed by standard library functions, so that the project's dependency list stays at three packages and CI stays fast.
16. As a maintainer, I want one analysis module rather than a processing layer per source, so that adding a seventh observability source means adding one function.
17. As a maintainer, I want anomaly thresholds expressed as named constants, so that a noisy production environment can be calibrated without editing logic.
18. As a maintainer, I want analysis to degrade to the previous behaviour when it meets an unexpected payload shape, so that an upstream API change reduces evidence quality instead of failing the investigation.
19. As a maintainer, I want the four places that format Evidence into prompt text reduced to one helper, so that changing the rendering cannot leave the Coordinator's prompts inconsistent with each other.
20. As a maintainer, I want the agent output schemas to contain only fields the LLM should genuinely produce, so that the boundary between computation and generation is legible from the schema alone.
21. As a maintainer, I want the new Evidence field to default to empty, so that existing Evidence construction sites and the current test suite continue to work unchanged.
22. As a contributor, I want the sources that were deliberately left on truncation to say why in a comment, so that I do not read the inconsistency as an unfinished job.
23. As a contributor, I want tests that assert on computed signals rather than on LLM prose, so that the suite fails for real regressions and not for rephrasing.
24. As a contributor, I want a test that proves computed facts reach the Coordinator's prompts, so that the rendering contract cannot silently break.
25. As an operator, I want telemetry payloads reduced to their salient facts before reaching the model, so that a high-volume incident costs fewer tokens rather than exceeding a context window.
26. As an operator, I want investigation cost to stay roughly flat as log volume grows, so that the noisiest incidents are not the most expensive to investigate.
27. As a reader evaluating the project, I want the pipeline documentation to show the analysis step, so that the design intent is visible without reading agent internals.

## Implementation Decisions

**A single analysis module, at the agent boundary.** One new module exposes one analysis function per high-volume source. It sits between the MCP client's response and the summarization prompt, inside each agent. This deliberately rejects the middleware/interceptor shape used by tool-calling agent frameworks: we have no tool-call loop to intercept, and per ADR-004 the agent already owns the translate → execute → summarize pipeline. Analysis is a fourth step in a pipeline that exists, not a new layer.

**Standard library only.** Distribution statistics, quantiles, and correlation come from the standard library's statistics module. Z-scores and rate-of-change are arithmetic. Prometheus range queries return hundreds of points, not millions, so array-library vectorization buys nothing at this data size and would add a heavyweight dependency to a three-package project.

**Scope is the three high-volume EvidenceSources.** Prometheus, Loki, and Tempo evidence gets analysis. Kubernetes, Deployment, and Documentation evidence keeps truncation — their payloads are short and semi-structured, and RAG chunks are prose where truncating is the correct behaviour. Each retained truncation site gets a comment recording that this is a decision.

**Computed fields leave the LLM output schemas.** The metrics anomaly flag, the log error count, and the trace slowest-service and bottleneck-span fields are removed from the agents' structured output schemas. Each schema retains its summary string. The LLM is no longer asked for a value that code can determine.

**Evidence gains a structured signals field.** A mapping of scalar facts, defaulting to empty, added to the Evidence model. Structured rather than prose so that the Memory store's archived incidents can be compared on facts, and so the field is usable by future rule-based logic without re-parsing. Defaulting to empty keeps every existing construction site valid. The full untruncated source response remains in the raw payload for audit; signals are the distilled view.

**One shared Evidence formatting helper.** Evidence is currently rendered into prompt text at four separate places — the hypothesis generation builder and three call sites in the Coordinator engine. All four route through a single helper that renders the summary and, when present, a facts line derived from signals. This converges duplicated formatting into one function, so the feature reduces a source of drift rather than adding one.

The prompt builders that take pre-formatted summary strings keep their signatures; the Coordinator passes them strings produced by the helper. This avoids reshaping the prompt-builder interfaces for a rendering change.

**Analysis never raises.** Each analysis function is defensive at the parse boundary. An unrecognized, empty, or malformed payload yields the previous truncated-text behaviour with empty signals. The existing agent-level exception handling stays as a backstop but must not be what catches a schema drift in a source API.

**Thresholds are named, tunable constants.** The anomaly z-score bound and the rate-of-change percentage are module-level constants with a comment marking them as calibration knobs. Real telemetry is noisy in environment-specific ways that a default cannot anticipate.

**Signals per source.** Metrics: anomaly flag, spike count, maximum z-score, p95, coefficient of variation. Logs: exact error count, total lines, distinct message groups. Traces: slowest service, slowest span, its duration, span count. Names are stable across sources where the concept is shared, so prompt rendering stays uniform.

## Testing Decisions

**What makes a good test here.** A test asserts on what leaves the module: the Evidence object an agent returns, and the prompt text the Coordinator produces. It does not reach into the analysis functions, because those are an implementation detail of the agent's pipeline — the same facts should hold if the statistics were computed differently. Critically, assertions target the signals mapping rather than the summary prose: prose comes from the mock LLM and asserting on it tests the fixture, not the system.

**Two seams, both already in use, no new ones.**

*Seam one — agent invocation to Evidence.* The existing MetricsAgent tests establish the pattern: a MockLLMProvider with registered responses, an inline stub client returning a canned payload, one call to the agent, assertions on the returned Evidence. Extended here with payloads engineered to known answers — a flat series versus a series with one injected spike, a burst of identical log lines with an exact known count, a three-level span tree with a known slowest span. Because the analysis runs inside this call, its correctness is verified transitively; it needs no tests of its own. Applied to the LogsAgent and TraceAgent, which currently have no tests, this seam also closes a coverage gap.

*Seam two — full Coordinator run to prompt text.* The MockLLMProvider already records every prompt it receives. A full investigation therefore verifies the rendering contract with a single assertion: that computed facts appear in a downstream prompt. Without it, the signals field could be populated correctly and rendered nowhere.

**Failure and edge cases** are covered at seam one: empty payloads, malformed payloads, and a source returning an error must each produce Evidence without raising, with empty signals.

**Existing constraints hold.** Per ADR-005 every external dependency stays mocked, no test touches the network, and the suite stays sub-second. The current suite must continue to pass, with changes limited to the MetricsAgent tests that assert on the removed schema field.

## Out of Scope

- **Rule-based pruning.** Letting the Coordinator refute a Hypothesis directly from signals, with no LLM call, is the natural next step and deliberately deferred — it changes engine control flow that the current tests pin down, and it should be a decision of its own.
- **The other three sources.** Kubernetes, Deployment, and Documentation evidence keeps truncation.
- **Persistence, API authentication, context-window summarization, and a conversational interface over completed RCAs.** All present in comparable projects, all judged low value here. The in-memory repository's restart behaviour should be documented as a deliberate boundary rather than fixed.
- **Any move toward a tool-calling agent loop.** ADR-003 stands; this spec strengthens it.
- **An evaluation harness** comparing RCA quality before and after. It is the only thing that would prove this improves outcomes rather than only improving inputs, and it is a substantially larger effort.
- **Forecasting, seasonality, and baseline learning.** Analysis describes the window it was given and does not model normal.

## Further Notes

The direct prior art is the output-transformer middleware in [OpenChoreo's SRE agent](https://github.com/openchoreo/openchoreo/tree/main/agents/sre-agent), which computes statistics, detects anomalies, builds span trees, and groups logs before results reach its model. The idea is theirs and worth crediting. The placement differs: they intercept tool results inside a ReAct loop because that is where their data flows; we compute at the agent boundary and feed a deterministic state machine. Their implementation reaches for an array library where the standard library suffices at this scale.

Two follow-ups keep the work from being invisible: the pipeline description in the README should show the analysis step, and an ADR should record this decision and the divergence above. The change is otherwise unobservable to anyone who does not read agent internals — which matters, because the reasoning is the most transferable part of it.
