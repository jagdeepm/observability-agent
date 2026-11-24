# Spec: Investigation Cost Guardrails

**Status:** ready-for-agent

## Problem Statement

Running the agent against a real LLM during e2e verification surfaced two failure modes that no unit test could catch, because the mocked test suite always returns small, fixed, hand-written responses — it never exercises what a real model actually chooses to do.

**A single investigation can be unbounded in cost.** `MAX_DEPTH` (`coordinator/engine.py`) bounds how deep the hypothesis tree grows — three levels — but nothing bounds how *wide* it gets at each level. Two generation points have no ceiling: `_phase_generate_hypotheses` makes one call and accepts however many hypotheses it returns, and `_deepen` makes one call *per confirmed hypothesis*, each again accepting an unbounded list of children. Neither the prompts (`coordinator/prompts.py`) nor the output schemas (`coordinator/outputs.py`) say how many. A live run reached 177 hypotheses and was still climbing. Growth compounds across the three depth levels — confirmed hypotheses at level N each independently spawn more at level N+1 — so it is not a rare edge case, it is the deterministic consequence of an ungoverned branching factor raised to an ungoverned confirm rate, three times.

**Aggregate spend has no ceiling at all.** Even if a single investigation stays reasonable, nothing stops the sum of many investigations from running past what an operator is willing to pay in a day or a week. Today the only way to notice is the LiteLLM proxy's own bill arriving after the fact.

Both were invisible until the agent ran against a real, generously-behaved model instead of a mock. This spec adds a ceiling to each.

## Solution

Two independent guardrails, addressing the two different questions "is this one investigation out of control?" and "are we spending too much this week regardless of why?"

**Hypothesis breadth cap.** Truncate what a single generate or deepen call is allowed to add to the tree, and bound how many confirmed hypotheses get deepened at all per level — the same shape of decision `MAX_DEPTH` already made for depth, made now for breadth. The prompts also ask for a bounded count, so a well-behaved model does not waste tokens generating hypotheses that will be discarded; truncation in code is the backstop for a model that ignores the ask.

**LLM token budget.** An in-memory counter tracks cumulative input+output tokens against two independently configured ceilings — tokens per day, tokens per week — sourced from the real usage numbers `pydantic-ai` already returns from every call and currently discards. Exceeding either ceiling raises a distinct, typed error before the next network call is made — not after — so tripping the budget costs nothing further. The Scheduler recognizes this error specifically and skips its normal three-retry dance, because retrying a budget failure is not just wasteful, it head-of-line-blocks every other queued incident behind three attempts that are guaranteed to fail identically.

Both are additive to the existing rule-based Coordinator (ADR-003): more rule, applied to a place the LLM has shown it cannot be trusted to self-limit, exactly the same argument ADR-007 already made for computing facts instead of asking for them.

## User Stories

1. As an on-call SRE, I want an investigation to converge in a bounded number of steps, so that a genuine incident is not still being investigated twenty minutes after it started.
2. As an operator, I want a single investigation's worst-case LLM call count to be a number I can compute from named constants, so that cost per incident is predictable rather than discovered after the fact.
3. As a maintainer, I want the hypothesis breadth cap to reuse the same pattern as `MAX_DEPTH` — a named, commented, tunable constant — so recalibrating it does not require reading the algorithm.
4. As a maintainer, I want truncation to keep the model's own first N hypotheses rather than re-ranking them, so the policy is legible and the dropped count is logged, not silently arbitrary.
5. As a maintainer, I want only the most-confident confirmed hypotheses deepened when there are more confirmed leaves than the cap allows, so the branches given more budget are the ones most likely to be true.
6. As an operator, I want a hard ceiling on total LLM token spend per day and per week, so a runaway investigation cannot silently run up an unbounded bill.
7. As an operator, I want the two ceilings to be independent, so a coarse weekly limit still catches sustained-but-not-spiky usage that never trips the daily one.
8. As an operator, I want both ceilings unset by default, so installing this change does not silently start rejecting LLM calls in an existing deployment.
9. As an on-call SRE, I want a budget-exceeded investigation to fail immediately and visibly, not retry three times and consume more of the queue's time before giving up.
10. As an on-call SRE, I want a budget-exceeded incident to say *why* it stopped, distinctly from an incident merely flagged stale for taking too long, so I don't waste time debugging the wrong cause.
11. As an operator, I want `/health` to show when the budget is exhausted, so I don't have to open individual incidents to notice.
12. As a maintainer, I want the budget check to cost nothing when it trips — no network call, no tokens — so a budget already at zero doesn't itself become an expensive way to fail.
13. As a maintainer, I want the token budget to live entirely in the real LLM provider, untouched by `MockLLMProvider`, so the existing unit suite stays fast, free, and deterministic exactly as ADR-005 requires.
14. As a contributor, I want the budget's ceiling math and window-rollover logic unit-testable with fake numbers and no real LLM call, so the feature has real test coverage rather than only integration-level trust.
15. As a maintainer, I want the in-memory nature of the budget counter stated as a deliberate, named limitation — resets on pod restart — rather than discovered by surprise the first time a crash-looping fault also quietly resets the safety net meant to catch it.

## Implementation Decisions

### Part 1 — Hypothesis breadth cap

**Three named constants, next to `MAX_DEPTH`.** All three live in `coordinator/engine.py` as module-level constants, following the existing `# ponytail: global default, configurable per-deployment if needed` convention already on `MAX_DEPTH`:

- `MAX_HYPOTHESES_PER_GENERATE` — cap on the root-generation call's output list.
- `MAX_CHILDREN_PER_DEEPEN` — cap on a single deepen call's output list.
- `MAX_DEEPEN_PER_LEVEL` — cap on how many confirmed hypotheses get deepened at all, per depth level.

With these at their proposed starting values (8 / 3 / 5), the worst case across three depth levels — the initial 8, then three rounds of up to 5×3 each — is roughly 8 + 3×(5×3) = 53 hypotheses total, down from the 100–200+ observed uncapped. The numbers are a starting point, not a claim of correctness — they are named and commented specifically so they can be recalibrated without touching the algorithm.

**Truncate in code; ask in the prompt.** Two changes, not one, because they solve different halves of the cost problem:

- `generate_hypotheses_prompt` and `deepen_hypotheses_prompt` (`coordinator/prompts.py`) gain an explicit count ("propose at most N hypotheses"). This is what stops a well-behaved model from *generating* the excess in the first place — it's the one lever that actually saves output tokens, not just discards them after paying for them.
- `_phase_generate_hypotheses` and `_deepen` (`engine.py`) truncate the returned list to the constant regardless of what the prompt asked for. Prompt compliance from a real model is not reliable enough to be the only control — this is the backstop that makes the cap a guarantee rather than a request. When truncation actually drops something, log the raw count vs. the kept count at INFO, so a maintainer watching logs sees the cap engage rather than wondering why a hypothesis they expected is missing.

**Keep the model's own first N; don't re-rank.** Truncation takes `output.hypotheses[:MAX_HYPOTHESES_PER_GENERATE]` (and the equivalent for children), preserving generation order. An autoregressive model settles on its most-considered items first; re-ranking by some other heuristic would be a second, unstated policy decision layered on top of a cap that's supposed to be simple and auditable.

**Select which confirmed hypotheses get deepened by confidence, not order.** `_deepen` currently iterates every confirmed leaf and makes one call per leaf — the more consequential of the two uncapped generation points, since it multiplies the number of *calls*, not just the size of one call's output. Change: sort confirmed leaves by `confidence` descending, take the top `MAX_DEEPEN_PER_LEVEL`. This directly bounds the number of deepen calls per level, and prioritizing the branches the evaluator was most confident about is also the substantively correct choice — those are the ones most worth a deeper look.

**No intake-level gate needed.** A tempting addition would be blocking the whole investigation early if the tree looks like it's about to explode. Not proposed here: with the caps in place, worst-case size is now a small, computable number (≈38, not ≈200), so the problem this would solve no longer exists at meaningful scale. Adding it would be solving a problem the simpler fix already removed.

### Part 2 — LLM token budget

**New module, `infrastructure/llm_budget.py`.** Two exports:

```python
class LLMBudgetExceededError(Exception):
    """Raised when a configured token ceiling has been reached.

    Carries which window tripped ("daily" or "weekly") and the numbers, so the
    message is actionable on its own: "LLM daily token budget exceeded:
    503,412 / 500,000 tokens used since 2026-08-14T00:00:00Z".
    """

class TokenBudget:
    """Tracks cumulative input+output tokens against two independent ceilings.

    record(input_tokens, output_tokens) — add usage from a completed call.
    raise_if_exceeded() — raise LLMBudgetExceededError if either window is
        already at or past its ceiling. Never records; a check is read-only.
    """
```

Kept as a small, standalone class with no dependency on `pydantic-ai` or `httpx` — the only inputs are integers and the clock — specifically so it is unit-testable without a real LLM call (see Testing Decisions).

**Two independent windows, calendar-aligned, checked lazily.** A "day" is UTC midnight to midnight; a "week" is UTC Monday midnight to the next. No background timer or scheduled reset — `TokenBudget` compares the current time against the last-recorded window boundary on every `record()`/`raise_if_exceeded()` call and rolls the relevant counter to zero if the boundary has passed. Calendar alignment over a rolling N-hours-from-first-use window because it is simpler to reason about ("resets at midnight UTC") and simpler for a human to explain when asked why an investigation stopped — a precise rate limiter is not the goal here, a coarse safety net is.

**Tokens counted: `input_tokens + output_tokens`, summed.** `pydantic-ai`'s `RunUsage` also exposes `cache_read_tokens`, `cache_write_tokens`, and audio token fields; this budget ignores them for a first pass — this deployment doesn't use audio, and cache tokens are typically priced differently (usually cheaper) than the ones counted here. Named as a simplification rather than hidden: if cache usage becomes material, the ceiling comparison is the one place that needs to change.

**Wired into `PydanticAIProvider` only — not the `LLMProvider` Protocol.** Real usage numbers exist only inside `PydanticAIProvider.generate_structured`, where `agent.run(...)` currently returns an `AgentRunResult` whose `.usage` is discarded after reading `.output`. `PydanticAIProvider.__init__` accepts an optional `budget: TokenBudget | None`, defaulting to one built from `LLM_TOKEN_BUDGET_DAILY` / `LLM_TOKEN_BUDGET_WEEKLY` env vars (both optional; unset means that window has no ceiling) when not passed — the same fallback-in-`__init__` shape `model`, `api_key`, and `base_url` already use two lines above it, rather than a fourth place (`create_llm_provider`) resolving this class's configuration. `generate_structured` calls `self._budget.raise_if_exceeded()` before `agent.run(...)`, and `self._budget.record(...)` from `result.usage` after a successful call. `MockLLMProvider` is untouched — it has no real token cost to track, and touching it would put a budget concept in the path of every unit test, which ADR-005 rules out. The `LLMProvider` Protocol's single method signature (`generate_structured`) does not change; this is an internal detail of one implementation, not a new contract every provider must satisfy.

**Ships disabled by default.** Both env vars unset → both ceilings `None` → `raise_if_exceeded()` never raises → current behavior is unchanged for every existing deployment. This is deliberate: a default numeric ceiling I picked without knowing an operator's actual tolerance would either be pointlessly loose (protects nothing) or wrongly tight (breaks a legitimate investigation), and either is worse than shipping off and letting the operator turn it on with a number they chose.

**The Scheduler skips retry for this one error, not others.** `_run_investigation` (`infrastructure/scheduler.py`) currently catches every exception the same way — log it, retry up to `max_retries` re-queued at the front, then `flag_stale()`. Add a specific `except LLMBudgetExceededError` clause, checked before the general `except Exception`, that skips straight to flagging the incident with no retry. Retrying here is not merely wasted effort — because the check happens before any network call, a retry costs zero extra tokens — but it does re-queue the incident at the *front* of the FIFO queue three more times, each cycle blocking every other queued incident behind an attempt that is certain to fail identically until the window resets.

**A distinct incident-level signal, not overloaded onto `is_stale`.** `flag_stale()`'s existing log message — "Incident has exceeded investigation time threshold" — is simply wrong for this cause and would mislead whoever reads it later. Add `Incident.llm_budget_exceeded: bool = False` and `flag_budget_exceeded()`, mirroring the existing `is_stale` field and `flag_stale()` method exactly, logging "Investigation halted — LLM token budget exceeded, human review needed." No new `IncidentStatus` enum value: the existing terminal/looping state machine (`IncidentStatus`, its `is_terminal` property, and every transition that reads it) is unchanged, kept to the same small, additive shape as the `is_stale` precedent it's copying rather than opening up the state machine for a cost concern that doesn't need a new lifecycle state.

**No separate intake gate in the Coordinator either — the existing error handling already does the right thing.** If the budget is already exhausted when an incident is dequeued, `_phase_explore`'s six concurrent agent calls each hit the budget check on their first LLM call, get caught by `_safe_call`'s existing `except Exception`, and return ordinary error Evidence — costing six real-but-cheap MCP calls to Prometheus/Loki/etc. and zero LLM tokens. The very next LLM call, in `_phase_generate_hypotheses`, is not wrapped in a handler and propagates `LLMBudgetExceededError` straight up through `Coordinator.run()` to the Scheduler's `_run_investigation`, which is exactly the seam already described above. The investigation stops within one phase of the ceiling being hit, with no new code needed to make that happen.

**`/health` reports budget state when a budget is configured.** `alert_receiver.py`'s health handler adds a `llm_budget` key, built with `getattr(scheduler.coordinator.llm, "budget_status", None)` — duck-typed and optional, so a mock-LLM deployment (which has no `TokenBudget` at all) sees no `llm_budget` key rather than a null placeholder. `PydanticAIProvider` gains a small `budget_status()` method returning today's and this week's used/limit numbers when a budget is configured.

## Testing Decisions

**What makes a good test here**, per source of risk, differs by part:

*Hypothesis breadth cap* is deterministic engine logic — the same seam the existing coordinator tests already use. A `MockLLMProvider` registered with a `GenerateHypothesesOutput`/`DeepenHypothesesOutput` deliberately larger than the cap proves truncation fires; asserting `len(result.hypotheses) <= <computable worst case>` after a multi-level run proves the caps compound correctly across depth, not just within one call. This is the same style as `test_targeted_evidence_gathered_concurrently`, added for the evidence-gathering concurrency fix: engineer the mock to be more generous than a real fixed-response test would ever be, specifically to exercise the cap.

*Token budget* splits into two seams because it splits into two kinds of code:

- `TokenBudget` itself is pure arithmetic and clock comparisons, with no LLM or network involved — tested directly, with a fake clock, exactly as a `Decimal`-based rate limiter would be tested anywhere else. Covers: recording accumulates correctly; `raise_if_exceeded()` raises only once a window's ceiling is met or passed; each window rolls over independently at its own boundary; a ceiling left `None` never raises regardless of usage.
- The wiring into `PydanticAIProvider` and the Scheduler's retry-skip is an integration seam, tested the way `test_retry_on_failure` already tests the Scheduler: a `MockCoordinator` (`tests/test_scheduler.py`) that raises `LLMBudgetExceededError` instead of the generic `RuntimeError` it raises today, asserting the incident is flagged via `flag_budget_exceeded()` after exactly one attempt, not the usual three.

**Failure and edge cases**, one per constant/config surface: a `GenerateHypothesesOutput` at exactly the cap (no truncation, no off-by-one); more confirmed leaves than `MAX_DEEPEN_PER_LEVEL` (only the top-K by confidence get a deepen call, and the rest correctly remain confirmed-but-not-deepened rather than silently dropped); a budget with only one of the two ceilings set; a budget check exactly at the ceiling boundary (inclusive — reaching the ceiling exactly should raise, not require exceeding it by one).

**Existing constraints hold.** Per ADR-005, `MockLLMProvider` and the rest of the mocked test suite are untouched by the token budget — it exists nowhere in a code path any current test exercises, so the sub-second, network-free suite stays sub-second and network-free. The hypothesis-cap tests extend the existing coordinator test file and pattern rather than introducing a new one.

## Out of Scope

- **Persistence of the token budget across restarts.** Explicitly deferred per this spec's request — the counter is in-memory only, and a pod restart (crash, OOM, rollout) resets it to zero. This mirrors the project's existing, deliberately-documented stance on `InMemoryIncidentRepository`: named as a boundary, not hidden as an oversight. Persisting it — a mounted file, or a ConfigMap the pod patches the way `task inject-fault` already does for flagd — is a follow-up if the in-memory gap proves to matter in practice.
- **Delegating budget enforcement to the LiteLLM proxy's own per-key `max_budget`/`budget_duration`.** Real, complementary, dollar-accurate where this token-based ceiling can only approximate cost — but it's pure external configuration on the proxy, not a change to this repository, and was discussed as a separate layer. Not part of this spec.
- **Dollar-cost estimation inside the agent.** Would require a hardcoded, drifting price-per-token table keyed by model name. Tokens are the honest unit available without that liability.
- **A rate limiter (requests-per-second/minute).** The token budget is a coarse, cumulative safety net against sustained overspend, not a burst-rate control — `PydanticAIProvider`'s existing 120s per-call timeout is the closest thing to burst protection this spec touches, and it already exists.
- **Re-ranking or scoring hypotheses by anything other than the evaluator's own confidence.** The breadth cap's selection policy (keep the model's first N; deepen the top-K by confidence) is deliberately the simplest defensible rule, not a new hypothesis-quality heuristic.
- **Changing `IncidentStatus` or the Hypothesis state machine.** Both guardrails are additive — a new boolean and a new truncation point — specifically so neither requires touching the state transitions the current test suite pins down.
- **Automatically resuming a budget-exceeded incident when the window resets.** It stays flagged until a human closes or re-triggers it; automatic resumption is a policy decision (should it re-investigate from scratch? from where it stopped?) this spec doesn't make.

## Further Notes

Both guardrails exist because of the same root cause, observed directly rather than anticipated: a real LLM, asked open-endedly to "propose hypotheses" or "drill deeper," does not share the restraint a hand-written mock response does. `ADR-005` (mock-everything testing) is exactly why this was invisible for as long as it was — every coordinator test registers a small, fixed response, so the unit suite has never once exercised a generation call behaving the way the real model behaves at scale. That isn't a flaw in ADR-005's reasoning (the suite needs to stay fast, free, and deterministic, and it does), it's a reminder that "the mock never does X" is not the same claim as "the real thing never does X" — the two guardrails here are what closes that specific gap for the two places it mattered.

The token budget's numbers deliberately ship unset. A number I choose without knowing what an operator considers acceptable weekly spend is a guess dressed up as a default; the honest version of this feature is the mechanism plus the dial, with the dial left at its off position until someone who knows the actual budget sets it.
