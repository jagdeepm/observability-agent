# Lesson 002: An agent will trust its own broken tooling as evidence

**Theme:** Agent reasoning failure modes
**Where this bit us:** `agents/logs.py` (LogsAgent), `mcp/deployment.py` (DeploymentClient)
**Cost of finding it late:** two real incidents, each root-caused to the wrong thing at 0.9+ confidence

## What happened

Twice, in the same debugging session, a live investigation confidently concluded the wrong root cause — not because the model reasoned badly, but because the agent handed it broken evidence and the model reasoned *correctly* from broken evidence.

**Case one.** LogsAgent's system prompt told the model: *"Use label selectors like `{service=\"name\"}` when a service is specified."* No Loki fed by Promtail has a `service` label — it labels by Kubernetes metadata (`namespace`, `pod`, `container`, `app`). The model, following the instruction it was given, wrote `{service="payment"} | json | level="error" or level="warn" or status="error" | json message | pattern \`config|connection|failed|error\`` — a query matching zero streams, compensating for the empty result with a `| pattern` stage Loki flatly rejects. Loki returned a 500. The agent reported that 500 as its Loki evidence. The investigation's conclusion: **"Loki log collection for payment service is not functioning or not configured," confidence 0.9**, remediation: restart Loki.

**Case two.** DeploymentClient had no `GITHUB_REPO` configured — expected, this cluster has no real deployment history to correlate against. But nothing stopped it from making the call anyway: it built `https://api.github.com/repos//commits` (empty repo name, real request, real public API) and got back a real 404. The agent reported that 404 as its deployment evidence. A second, unrelated investigation into the same underlying incident concluded: **"Environment variable GITHUB_REPOSITORY or GITHUB_OWNER is not set... causing the deployment service to fall back to null values," confidence 0.96**, remediation: set the env var and restart.

In both cases the actual incident (a payment service genuinely failing) was still sitting there, unexamined, while the agent produced a plausible, well-written, wrong RCA about its own plumbing.

## Root cause

Same shape both times: **the agent's own tooling produced a real, well-formed error response, and nothing distinguished "the target system is broken" from "my query/config was broken."** An LLM asked to summarize an error will summarize it fluently regardless of whose fault it was — it has no way to know the 500 came from a malformed query it was told to write, or that the 404 came from a URL with a hole in it where a repo name should be.

## The fix

- LogsAgent now discovers the real label names via `list_labels()` and puts them in the prompt: *"The only label names in this Loki are: {labels}. Use nothing else."*
- DeploymentClient refuses to build the URL at all when unconfigured, raising a clear `"Deployment history is not configured: set GITHUB_REPO..."` instead of making a request that was never going to mean anything.

Neither fix required the model to reason better. Both fixes stopped feeding it a lie.

## The generalizable lesson

**A tool call failing is not neutral information — the LLM will make a story out of it, and the story will sound exactly as confident as a story built on real evidence.** Traditional software fails loudly and stops; an LLM summarizing a failure produces *prose*, and prose reads as an explanation whether or not it's grounded in anything real. The two most dangerous failure sources are the ones the agent controls itself: a prompt that gives wrong instructions about the environment it's actually running in, and an integration that's allowed to make a "successful" network call while unconfigured.

If you're building an agent: audit every place your own code can produce a response that looks like data but is actually just describing your own bug. Fail those loud and early, before the summarization step ever sees them — a clean `RuntimeError("not configured")` an agent can catch and label as "no evidence available" is infinitely safer than a 404 the model will happily interpret as a root cause.
