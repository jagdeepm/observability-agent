# Lessons Learned — Building an AI Observability Agent

This is a running record of the real pitfalls hit while building this project, kept for two reasons: so the same mistake doesn't get made twice on this codebase, and so there's a source of truth ready when it's time to write a talk on "what building an agent actually taught us."

Each entry is one concrete, evidenced incident — what happened, why, what fixed it, and the lesson generalized past this specific codebase. Numbered like this project's ADRs (`docs/adr/`), for the same reason: one file per thing worth remembering, so adding the next one is a clean, reviewable diff rather than an edit war on a growing document.

**How an entry gets added:** run `/log-lesson` at the end of a session where something instructive happened — a bug that survived review, a design decision that turned out wrong (or right) once it met reality, a piece of infrastructure that behaved differently than the docs implied. See `.claude/skills/log-lesson/SKILL.md`.

## Index

| # | Lesson | Theme |
|---|---|---|
| [001](001-mocked-tests-cannot-catch-what-they-do-not-imagine.md) | Mocked tests can't catch what they don't imagine | Testing agentic systems |
| [002](002-an-agent-will-trust-its-own-broken-tooling.md) | An agent will trust its own broken tooling as evidence | Agent reasoning failure modes |
| [003](003-llm-calls-need-the-same-reliability-discipline-as-any-network-call.md) | LLM calls need the same reliability discipline as any other network call | Reliability engineering for agents |
| [004](004-real-llm-cost-is-a-design-surface-not-an-afterthought.md) | Real LLM cost is a design surface, not an afterthought | Cost as a reliability concern |
| [005](005-live-infrastructure-has-truths-no-code-review-finds.md) | Live infrastructure has truths no code review finds | Verification — running the real thing |

## For the presentation

The five entries above cluster into a natural three-act structure, if a talk is what this is for:

1. **"Your tests are lying to you, a little"** (001) — mocking is right for speed, and it means you've only tested a system as generous as the person who wrote the mocks. Opens with the 177-hypothesis live run as the hook.
2. **"The agent believes what its own tools tell it"** (002, 005) — two stories where the agent's conclusion was fluent, confident, and wrong, because the evidence feeding it was broken by the agent's own tooling or by infrastructure nobody had queried live yet. Good pairing: 002 is application-level (a bad prompt), 005 is infrastructure-level (a Helm default) — same underlying failure, two different layers.
3. **"An agent is a metered, unreliable dependency you're building on top of"** (003, 004) — timeouts, retries, and cost aren't separate from agent design, they're the same reliability discipline applied to a new kind of call. Closes with the real number: one afternoon of honest debugging, $7.50, and what that revealed about unbounded breadth.

Every entry cites the actual commit and the actual numbers — no lesson here is a platitude arrived at without a real incident behind it. Pull directly from the "What happened" sections for slide content; they're written to stand alone.

## Related reading

- `docs/adr/` — the architectural decisions this project made, several of which these lessons pressure-tested (ADR-003's rule-based coordinator, ADR-005's mock-everything testing).
- `docs/specs/investigation-cost-guardrails.md` — the spec that came out of lessons 001 and 004.
- `docs/e2e-testing.md`, "Known limitations" — the live status of what's bounded and what isn't, updated as these lessons land fixes.
