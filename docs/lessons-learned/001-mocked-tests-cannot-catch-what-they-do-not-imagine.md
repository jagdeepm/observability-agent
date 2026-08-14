# Lesson 001: Mocked tests can't catch what they don't imagine

**Theme:** Testing agentic systems
**Where this bit us:** Coordinator hypothesis generation, `coordinator/engine.py`
**Cost of finding it late:** an investigation that ran 20–40+ minutes, once 40+ with no result at all

## What happened

The unit suite is fully mocked by design (ADR-005) — no test may touch a network or a live cluster. Every coordinator test registers a `MockLLMProvider` with a small, hand-typed response: `test_full_happy_path` proposes exactly 2 hypotheses, evaluates exactly 2, deepens with an empty list. That's the correct choice for the suite's job — sub-second, deterministic, free.

But it also meant one entire behavior of the system was never exercised: what happens when the thing generating hypotheses is *generous*. Hypothesis generation and "drill deeper" prompts had no count in them — "propose root-cause hypotheses," "propose child hypotheses that explain the mechanism" — and the output schemas had no `max_length`. A mock never tests this path because a human wrote the mock's answer and humans don't accidentally write 40 hypotheses into a test fixture.

A real model, asked the same open-ended question, does not share that restraint. Run against a real LiteLLM-backed model:

- One investigation reached **104 hypotheses**.
- A separate one reached **215**, taking 487 seconds to converge on a confidently wrong conclusion (see Lesson 002).
- A third, after an unrelated concurrency fix had already cut typical run time roughly 10×, still climbed **29 → 53 → 61 → 107 → 125 → 177** hypotheses across successive checks — genuine, continuous progress, just wide.

None of this showed up in 100+ passing unit tests. It only showed up the first time the agent ran against a real model. (A fourth investigation, separately, didn't converge at all — it hung outright. That's a related but distinct failure, covered in Lesson 003.)

## Root cause

Two generation points had no ceiling, and growth between them was multiplicative, not additive:

1. `_phase_generate_hypotheses` — one call, unbounded list.
2. `_deepen` — one call **per confirmed hypothesis** (not batched), each again unbounded.

`MAX_DEPTH = 3` was a real, named, commented constant — someone had deliberately bounded how *deep* the tree could grow. Nobody had made the equivalent decision for how *wide* it could get at each level, because the axis that needed bounding never showed itself under mocked responses.

## The fix

Two more named constants next to `MAX_DEPTH` — `MAX_HYPOTHESES_PER_GENERATE`, `MAX_CHILDREN_PER_DEEPEN`, `MAX_DEEPEN_PER_LEVEL` — enforced in code (a truncation the model can't talk its way past) with the prompt also asking for a bounded count (so a well-behaved model doesn't waste tokens generating the excess in the first place). Deepening was changed to select the *most confident* confirmed leaves rather than *every* confirmed leaf, since that's the more consequential cap — it bounds the number of LLM calls, not just the size of one call's output.

Worst case across three depth levels dropped from the 100–200+ observed to a computable 8 + 3×(5×3) = 53.

See `docs/specs/investigation-cost-guardrails.md`, commit `c8fdfaa`.

## The generalizable lesson

**Mocking everything external is the right call for test speed and determinism — and it guarantees your test suite has an opinion only as generous as the person who wrote the mock.** A test suite built entirely on hand-typed LLM responses will never generate a response wider, weirder, or more enthusiastic than a human bothered to type. If any part of your system's cost or correctness depends on *how much* a model decides to produce — not just *what* — that dimension needs at least one real run before you can claim it's bounded. "Green tests" and "bounded behavior" are different claims when the thing under test is a model with its own judgment.

If you're building an agent: for every place you ask a model "how many," write down the number you're willing to accept, and enforce it in code — not just in the prompt. The prompt is a request. The code is the guarantee.
