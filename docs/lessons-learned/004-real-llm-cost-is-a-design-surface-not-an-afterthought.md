# Lesson 004: Real LLM cost is a design surface, not an afterthought

**Theme:** Cost as a reliability concern
**Where this bit us:** the whole debugging session — $7.50, 3,847,457 tokens, one afternoon
**Cost of finding it late:** the bill itself, and the discovery that came with it

## What happened

Verifying this project's e2e pipeline against a real LiteLLM proxy meant repeatedly triggering full, real investigations — deliberately, to reproduce and confirm-fix bugs, and less deliberately, watching a stuck investigation run for tens of minutes after the interesting diagnostic information had already been extracted. The session's LiteLLM usage came to 3.8M tokens and $7.50 — small in absolute terms, but entirely attributable to a handful of causes, once traced:

- **Unbounded hypothesis breadth** (Lesson 001): investigations reaching 100–200+ hypotheses meant 100–200+ real LLM calls per run, several times over.
- **A retry storm** (Lesson 003, before it was fixed): a failed investigation restarting from scratch — full re-explore, full re-hypothesize — up to four times.
- **Debugging habit**: re-running the same live scenario through multiple verification paths (a direct probe, then the actual pytest e2e test, then again after a fix) rather than treating one confirmed reproduction as enough.

None of this was reckless — it's exactly what verifying a real system against a real dependency requires. But it made concrete something easy to intellectualize and easy to under-budget for: **every investigation this agent runs costs real money, and the amount is not obviously bounded unless someone bounds it.**

## Root cause

Cost had no place in the design until it showed up on an invoice. Nothing in the coordinator asked "how much is this about to cost" before making the next call; nothing tracked cumulative spend; nothing distinguished "this investigation is progressing slowly" from "this investigation is about to be very expensive." The two are often the same symptom, discovered the same way — too late to prevent, only in time to explain.

## The fix

Two independent guardrails, deliberately not one:

- **The breadth cap** (Lesson 001) bounds the cost of any *single* investigation — the mechanism-level fix.
- **A token budget** — `TokenBudget`, tracking cumulative input+output tokens against two independent ceilings (tokens/day, tokens/week), checked before every call, raising before the network request rather than after. Ships with **both ceilings unset by default** — a number picked without knowing an operator's real tolerance is a guess wearing a default's clothes, and shipping a wrong default is worse than shipping no default. See `docs/specs/investigation-cost-guardrails.md`.

The two answer different questions on purpose: "is any single investigation out of control?" versus "are we spending too much this week, regardless of why?" A system can pass the first check and still fail the second if it just runs *many* well-behaved investigations.

## The generalizable lesson

**Cost is a reliability property, not a finance-team concern that happens after the fact.** An agentic system that calls a metered API has a resource it can exhaust the same way it can exhaust memory or file descriptors — the difference is that exhausting the token budget doesn't crash the process, it just quietly keeps working and keeps costing money, which makes it easy to defer thinking about until the bill arrives. Design for it the way you'd design for any other exhaustible resource: a ceiling, a clear failure mode when the ceiling is hit, and a human notified rather than a process retrying blindly into the same wall.

If you're building an agent: before your first real run against a paid endpoint, know the answer to "what's the worst this could cost in the next hour if something goes wrong" — and make sure the answer is a number you computed, not a number you'll discover.
